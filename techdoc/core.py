"""S4-A minimal local RAG integration. BM25 is a *real lexical demo*, NOT Dense retrieval.

Blocks and source spans are the evidence authority; chunks are disposable derived data.
Document parsing is conservative; unsupported Office objects and unreliable tables are excluded.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
import json, math, os, re, tempfile, threading
import copy
from typing import Any

from .evidence_gate import Candidate, EvidenceGate, Request
from .s3b_generator import OpenAIChatClient, S3BEngine
from .retrieval import fuse_rrf
from .excerpts import select_excerpts
from .index_views import IndexViews,FrozenCounts,freeze,plain
from .inference import inference_slot,configured_threads

class RetrieverBuildFailed(RuntimeError):
    """Required retrieval index could not be built."""


@dataclass(frozen=True)
class PreparedGeneration:
    """Request-owned evidence and metadata, captured while the index is locked."""
    run: Any
    metadata: dict
    snapshot_epochs: tuple

MAX_BYTES = 8 * 1024 * 1024
MAX_BLOCKS = 20000
MAX_CHARS = 3000000


def digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def block(snapshot: str, i: int, kind: str, text: str, section: str,
          locator: dict, *, indexable: bool = True, verified: bool = False) -> dict:
    return {"block_id": digest([snapshot, i, kind, text, locator])[:20],
            "snapshot_id": snapshot, "kind": kind, "text": text, "section": section,
            "locator": locator, "indexable": indexable, "verified": verified}


class DocParser:
    EXTENSIONS = {'.pdf', '.docx', '.md', '.txt', '.html', '.htm'}

    @staticmethod
    def parse(name: str, data: bytes, version: str = '') -> tuple[dict, list[dict]]:
        ext = Path(name).suffix.lower()
        if ext not in DocParser.EXTENSIONS:
            raise ValueError('UNSUPPORTED_FORMAT')
        if not data or len(data) > MAX_BYTES:
            raise ValueError('EMPTY_OR_OVERSIZED_UPLOAD')
        if len(version) > 120:
            raise ValueError('VERSION_TOO_LONG')
        basename = Path(name.replace('\\', '/')).name
        if not basename or len(basename) > 200:
            raise ValueError('BAD_FILENAME')
        source_sha = sha256(data).hexdigest()
        snap = digest([basename, version, source_sha]+(['md_structure_v2'] if ext=='.md' else []))[:28]
        warnings: list[str] = []
        blocks: list[dict] = []

        def add(kind, text, section, locator, indexed=True, verified=False):
            blocks.append(block(snap, len(blocks), kind, str(text), section, locator,
                                indexable=indexed, verified=verified))

        if ext in ('.md', '.txt'):
            raw = data.decode('utf-8-sig', errors='strict')
            section = basename
            fence=None;code=[];code_start=None
            def code_flush():
                if code:add('code','\n'.join(code),section,{'line':code_start},verified=True)
            for i,line in enumerate(raw.splitlines(),1):
                stripped=line.strip()
                mark=re.match(r'^(`{3,}|~{3,})(.*)$',stripped) if ext=='.md' else None
                if fence is not None:
                    code.append(line)
                    if mark and mark[1][0]==fence[0] and len(mark[1])>=fence[1] and not mark[2].strip():
                        code_flush();code=[];fence=None
                    continue
                if mark:
                    fence=(mark[1][0],len(mark[1]));code=[line];code_start=i;continue
                if not stripped:continue
                heading=re.match(r'^(#{1,6})\s+(.+)$',stripped) if ext=='.md' else None
                if heading:
                    section=heading[2].strip();add('heading',section,section,{'line':i},verified=True)
                    blocks[-1]['heading_level']=len(heading[1])
                elif ext=='.md' and re.match(r'^(?:[-+*]|\d+[.)])\s+',stripped):
                    add('list',line,section,{'line':i},verified=True)
                else:add('paragraph',stripped,section,{'line':i},verified=True)
            if fence is not None:
                code_flush();warnings.append('MARKDOWN_UNCLOSED_FENCE')
        elif ext in ('.html', '.htm'):
            # Intentionally ignores script/style and retains text as HTML block text;
            # do not claim visual layout/complex tables have been recovered.
            raw = data.decode('utf-8-sig', errors='strict')
            p = _BasicHTML()
            p.feed(raw); p.close()
            section = basename
            for i, (kind, text) in enumerate(p.parts):
                if kind == 'heading': section = text
                add(kind, text, section, {'html_item': i}, verified=False)
            warnings.append('HTML_STRUCTURE_LIMITED')
        elif ext == '.docx':
            from io import BytesIO
            from zipfile import ZipFile, BadZipFile
            # Limit compressed Office package expansion before handing it to python-docx.
            try:
                with ZipFile(BytesIO(data)) as z:
                    info=z.infolist()
                    if len(info)>2000 or sum(x.file_size for x in info)>32*1024*1024:
                        raise ValueError('DOCX_EXPANSION_LIMIT')
            except BadZipFile:
                raise ValueError('INVALID_DOCX_PACKAGE') from None
            from docx import Document
            from docx.oxml.ns import qn
            doc = Document(BytesIO(data))
            section = basename
            for i, obj in enumerate(doc.iter_inner_content()):
                if hasattr(obj, 'rows'):
                    tbl = obj
                    # Vertical/horizontal merges cannot safely be flattened by python-docx.
                    merged = bool(tbl._tbl.xpath('.//w:gridSpan') or tbl._tbl.xpath('.//w:vMerge'))
                    if merged:
                        add('table_unverified', '[Complex merged table omitted]', section,
                            {'table_order': i}, indexed=False)
                        warnings.append('DOCX_MERGED_TABLE_EXCLUDED')
                        continue
                    for j, row in enumerate(tbl.rows):
                        cells = [c.text.strip().replace('\n', ' ; ') for c in row.cells]
                        val = ' | '.join(cells).strip()
                        if val:
                            add('table_row', val, section, {'table_order': i, 'row': j}, verified=False)
                else:
                    text = obj.text.strip()
                    if text:
                        is_heading = obj.style is not None and str(obj.style.name).lower().startswith('heading')
                        if is_heading: section = text
                        add('heading' if is_heading else 'paragraph', text, section,
                            {'paragraph_order': i}, verified=False)
                    # Detect native chart/diagram/image references even if text is empty.
                    nodes = obj._p.xpath('.//c:chart') + obj._p.xpath('.//a:blip')
                    if nodes:
                        add('figure_unverified', '[Office visual object omitted]', section,
                            {'paragraph_order': i, 'object_count': len(nodes)}, indexed=False)
                        warnings.append('DOCX_VISUAL_OBJECT_EXCLUDED')
        elif ext == '.pdf':
            from io import BytesIO
            import pymupdf
            doc = pymupdf.open(stream=data, filetype='pdf')
            if doc.needs_pass:
                raise ValueError('ENCRYPTED_PDF_UNSUPPORTED')
            if doc.page_count > 200:
                raise ValueError('PDF_TOO_MANY_PAGES')
            for pi, page in enumerate(doc, 1):
                # Treat page as a hard boundary while visual reading order is unverified.
                section = f'{basename} · page {pi}'
                bad_areas = []
                try:
                    tab = page.find_tables()
                    for t in tab.tables:
                        # A single short row often comes from highlighted inline text,
                        # not a meaningful grid. Retain its original positioned text.
                        if t.row_count==1 and t.col_count<=2 and sum(len(c or '') for row in t.extract() for c in row)<40:
                            warnings.append('PDF_DEGENERATE_TABLE_AS_POSITIONED_TEXT')
                            continue
                        bad_areas.append(t.bbox)
                except Exception:
                    warnings.append('PDF_TABLE_DETECTION_UNAVAILABLE')
                for bi, v in enumerate(page.get_text('blocks', sort=True)):
                    if len(v)>6 and v[6]!=0:
                        add('figure_unverified', '[PDF image excluded]', section,
                            {'page':pi,'block_index':bi}, indexed=False)
                        warnings.append('PDF_IMAGE_CONTENT_EXCLUDED')
                        continue
                    x0, y0, x1, y1, text = v[:5]
                    s = str(text).strip()
                    if not s:
                        continue
                    rect = (x0,y0,x1,y1)
                    intersect = any(_intersect(rect, other) for other in bad_areas)
                    add('table_unverified' if intersect else 'paragraph', s, section,
                        {'page': pi, 'bbox': [round(x,1) for x in rect], 'block_index':bi},
                        indexed=not intersect, verified=False)
                    if intersect: warnings.append('PDF_TABLE_CONTENT_EXCLUDED')
                if page.get_drawings():
                    warnings.append('PDF_VECTOR_DRAWINGS_NOT_INTERPRETED')
            warnings.append('PDF_READING_ORDER_UNVERIFIED')

        if len(blocks)>MAX_BLOCKS or sum(len(b['text']) for b in blocks)>MAX_CHARS:
            raise ValueError('DOCUMENT_COMPLEXITY_LIMIT')
        if not any(b['indexable'] and b['text'] for b in blocks):
            raise ValueError('NO_INDEXABLE_CONTENT')
        metadata = {"snapshot_id": snap, "filename": basename, "product_version": version,
                    "source_sha256": source_sha, "parser": 's4a_conservative_pdf_v2' if ext=='.pdf' else ('s4a_conservative_md_structure_v2' if ext=='.md' else f"s4a_conservative_{ext[1:]}"),
                    "block_count": len(blocks), "indexed_block_count": sum(b['indexable'] for b in blocks),
                    "warnings": sorted(set(warnings))}
        return metadata, blocks


def _intersect(a, b):
    l = max(0., min(a[2],b[2])-max(a[0],b[0]))
    h = max(0., min(a[3],b[3])-max(a[1],b[1]))
    if not l or not h: return False
    return l*h > 0.2*max(1, (a[2]-a[0])*(a[3]-a[1]))


class _BasicHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[];self.skip=0;self.tag=[];self.buffer=[]
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.skip+=1
        if tag in ('h1','h2','h3','h4','h5','h6','p','li'):
            self.tag.append(tag);self.buffer.append([])
    def handle_data(self, data):
        if not self.skip and self.buffer:self.buffer[-1].append(data)
    def handle_endtag(self, tag):
        if tag in ('script','style') and self.skip: self.skip-=1
        if self.tag and self.tag[-1]==tag:
            text=' '.join(''.join(self.buffer.pop()).split())
            self.tag.pop()
            if text:self.parts.append(('heading' if tag.startswith('h') else 'paragraph',text))


def make_chunks(blocks: list[dict], cap: int = 550) -> list[dict]:
    """Bounded character proxy chunks; NOT tokenizer-aware. Hard stop at failed blocks."""
    if cap < 100: raise ValueError('MIN_CHARS_100')
    out=[];current=[];section=None;snapshot=None
    def flush():
        nonlocal current
        if current:
            parts=[]; spans=[]
            for b, a, z in current:
                if parts:parts.append('\n')
                parts.append(b['text'][a:z])
                spans.append({'block_id': b['block_id'], 'start':a,'end':z})
            body=''.join(parts)
            cid=digest([snapshot,section,cap,spans,body])[:24]
            out.append({'chunk_id':cid,'snapshot_id':snapshot,'section':section,
                        'body_text':body,'index_text':body,'source_spans':spans,
                        'chunk_chars':len(body),'tokenizer_id':'NONE__CHAR_PROXY'})
            current=[]
    for b in blocks:
        if not b['indexable'] or not b['text']:
            flush();section=None;snapshot=None
            continue
        if (snapshot,section)!=(b['snapshot_id'],b['section']):
            flush();snapshot=b['snapshot_id'];section=b['section']
        t=b['text'];pos=0
        while pos <len(t):
            used = sum(z-a for _,a,z in current)+max(0,len(current)-1)
            # A new source span inserts one separator newline on serialization.
            # Reserve that byte before deciding how much of this block fits.
            separator = 1 if current else 0
            room=cap-used-separator
            if room<=0:
                flush();continue
            end=min(len(t),pos+room)
            if end<len(t) and end-pos>30:
                at=max(t.rfind(' ',pos+10,end),t.rfind('\n',pos+10,end))
                if at>pos+10:end=at+1
            current.append((b,pos,end));pos=end
            if pos<len(t):flush()
    flush()
    ids=[x['chunk_id'] for x in out]
    if len(ids)!=len(set(ids)):raise ValueError('CHUNK_ID_COLLISION')
    return out


WORD=re.compile(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+',re.UNICODE)

def tokens(text: str) -> list[str]:
    out=[]
    for m in WORD.finditer(text.lower()):
        t=m.group()
        if '\u4e00'<=t[0]<='\u9fff':
            out.extend(t[i:i+2] for i in range(len(t)-1))
            if len(t)==1:out.append(t)
        else:out.append(t)
    return out


class LexicalIndex:
    """Self-contained BM25 lexical *demo*, not a fallback called Dense."""
    def __init__(self, chunks):
        self.chunks=list(chunks)
        self.frequencies=[Counter(tokens(x['index_text'])) for x in chunks]
        self.lengths=[sum(f.values()) for f in self.frequencies]
        self.avgdl=sum(self.lengths)/max(len(self.lengths),1)
        self.df=Counter()
        for f in self.frequencies:self.df.update(f.keys())
    @classmethod
    def from_previous(cls,chunks,previous):
        """Reuse unchanged token counts; update global df/N/avgdl exactly."""
        value=object.__new__(cls);value.chunks=list(chunks)
        old={c['chunk_id']:(c,tf,length) for c,tf,length in zip(previous.chunks,previous.frequencies,previous.lengths)}
        current={c['chunk_id']:c for c in chunks};df=Counter(previous.df)
        for cid,(chunk,tf,_) in old.items():
            if cid not in current or current[cid]['index_text']!=chunk['index_text']:df.subtract(tf.keys())
        value.frequencies=[];value.lengths=[]
        for chunk in chunks:
            row=old.get(chunk['chunk_id'])
            if row and row[0]['index_text']==chunk['index_text']:tf,length=row[1:]
            else:
                tf=Counter(tokens(chunk['index_text']));length=sum(tf.values());df.update(tf.keys())
            value.frequencies.append(tf);value.lengths.append(length)
        value.df=Counter({word:count for word,count in df.items() if count>0})
        value.avgdl=sum(value.lengths)/max(len(value.lengths),1)
        return value
    def query(self, query, snapshot_id, topk=6):
        q=set(tokens(query));N=len(self.chunks)
        if not q:return []
        found=[]
        for ch,tf,dl in zip(self.chunks,self.frequencies,self.lengths):
            if isinstance(snapshot_id,frozenset):
                if ch['snapshot_id'] not in snapshot_id:continue
            elif snapshot_id is not None and ch['snapshot_id']!=snapshot_id:continue
            s=0.
            for term in q:
                if not tf[term]:continue
                idf=math.log1p((N-self.df[term]+.5)/(self.df[term]+.5))
                s+=idf*tf[term]*2.2/(tf[term]+1.2*(.25+.75*dl/max(self.avgdl,1)))
            if s>0: found.append((s,ch))
        found.sort(key=lambda x:(-x[0],x[1]['chunk_id']))
        return [{'score':round(s,6), 'chunk':ch} for s,ch in found[:topk]]

    def query_scope(self,question,snapshot_ids,*,required_snapshots=frozenset(),topk=32):
        from .knowledge_scope import MAX_ALLOWED_SOURCES,bounded_candidates
        selected=frozenset(snapshot_ids);required=frozenset(required_snapshots)
        if not 1<=len(selected)<=MAX_ALLOWED_SOURCES or not required<=selected or len(required)>4:
            raise ValueError('LEXICAL_BAD_KNOWLEDGE_SCOPE')
        hits=self.query(question,selected,topk=topk)
        for sid in sorted(required):hits.extend(self.query(question,sid,topk=4))
        return bounded_candidates(hits,required,limit=64)


class DocumentStore:
    def __init__(self, path: Path, cap: int = 550):
        from .runtime_config import llm_timeout_seconds
        self.llm_timeout_s=llm_timeout_seconds()
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
        self.originals=self.path/'originals';self.originals.mkdir(exist_ok=True)
        self.cap=cap;self.lock=threading.RLock();self.docs={};self.blocks={};self.chunks=[]
        self._index_views=IndexViews(max_retired=int(os.getenv('TECHDOC_INDEX_MAX_RETIRED_VIEWS','2')),
                                    max_bytes=int(os.getenv('TECHDOC_INDEX_VIEW_MAX_BYTES',str(256*1024*1024))))
        self._snapshot_epochs={};self._next_snapshot_epoch=0
        self.retriever_mode=os.getenv('TECHDOC_RETRIEVER','bm25').lower()
        if self.retriever_mode not in ('bm25','dense','hybrid'):
            raise ValueError('UNSUPPORTED_RETRIEVER_MODE')
        self.query_plan_policy=os.getenv('TECHDOC_QUERY_PLAN','shadow')
        if self.query_plan_policy not in ('off','shadow'):raise ValueError('BAD_QUERY_PLAN_POLICY')
        self.context_policy=os.getenv('TECHDOC_CONTEXT_POLICY','legacy')
        if self.context_policy not in ('legacy','structured'):raise ValueError('BAD_CONTEXT_POLICY')
        self.generation_policy=os.getenv('TECHDOC_GENERATION_POLICY','legacy')
        from .generation import policy_identity
        policy_identity(self.generation_policy)
        self.dense=None
        self.retrieval_ready=False
        index_policy=os.getenv('TECHDOC_INDEX_POLICY','legacy')
        if index_policy not in ('legacy','staged_v1'):raise ValueError('BAD_INDEX_POLICY')
        if index_policy=='staged_v1':
            from .inference import enabled
            if not enabled():raise ValueError('STAGED_INDEX_REQUIRES_INFERENCE_BUDGET')
        configured_threads()
        with inference_slot():self._load()
        self.reranker=None
        if os.getenv('TECHDOC_RERANKER_MODEL'):
            from .inference import policy
            if policy()=='isolated_v1':
                from .rerank_process import RerankProcess
                self.reranker=RerankProcess(os.environ['TECHDOC_RERANKER_MODEL'])
            else:
                from .retrieval import CrossEncoderReranker
                self.reranker=CrossEncoderReranker(os.environ['TECHDOC_RERANKER_MODEL'])
            if hasattr(self.reranker,'score_cache'):
                self.reranker.score_cache.prune(self.chunks);self.reranker.score_cache.flush()
    def _load(self):
        self._recover_legacy_ingests()
        # Files with this exact temporary prefix are never committed snapshots.
        # After a crash between tempfile creation and os.replace, ignore and
        # discard them before globbing *.json. Single-process deployment only.
        for tmp in self.path.glob('.tmp_*.json'):
            if tmp.is_file():tmp.unlink()
        for tmp in self.originals.glob('.tmp_*'):
            if tmp.is_file():tmp.unlink()
        # Recover a deletion interrupted after metadata was moved out of *.json.
        # The tombstone contains the original validated snapshot metadata.
        for marker in sorted(self.path.glob('*.deleting')):
            sid=marker.stem
            if not re.fullmatch(r'[0-9a-f]{28}',sid):
                raise ValueError('INVALID_DELETE_TOMBSTONE')
            obj=json.loads(marker.read_text(encoding='utf-8'))
            meta=obj['metadata']
            if meta['snapshot_id']!=sid:
                raise ValueError('CORRUPT_DELETE_TOMBSTONE')
            ext=Path(meta['filename']).suffix.lower()
            if ext not in DocParser.EXTENSIONS:
                raise ValueError('BAD_DELETE_TOMBSTONE_EXTENSION')
            (self.originals/(sid+ext)).unlink(missing_ok=True)
            marker.unlink()
        for p in sorted(self.path.glob('*.json')):
            obj=json.loads(p.read_text(encoding='utf-8'))
            meta,blocks=obj['metadata'],obj['blocks']
            if p.stem!=meta['snapshot_id'] or any(b['snapshot_id']!=p.stem for b in blocks):
                raise ValueError('CORRUPT_STORED_DOCUMENT')
            self.docs[p.stem]=meta
            self.blocks[p.stem]=blocks
            self._stamp_snapshot(p.stem)
        self._rebuild()

    def _recover_legacy_ingests(self):
        # Only a journal created before publishing a NEW legacy snapshot can
        # authorize removal; never infer ownership from an unreferenced file.
        for writing in self.path.glob('.tmp_ingest_*.journal.writing'):
            if writing.is_symlink() or not re.fullmatch(r'\.tmp_ingest_[0-9a-f]{28}\.journal\.writing',writing.name):raise ValueError('INVALID_INGEST_JOURNAL')
            writing.unlink()
        for marker in self.path.glob('.tmp_ingest_*.journal'):
            if marker.is_symlink() or marker.stat().st_size>4096:raise ValueError('INVALID_INGEST_JOURNAL')
            record=json.loads(marker.read_text(encoding='utf-8'));sid=record['snapshot_id']
            if (not re.fullmatch(r'[0-9a-f]{28}',sid) or marker.name!='.tmp_ingest_'+sid+'.journal'
                or record['extension'] not in DocParser.EXTENSIONS
                or any(not re.fullmatch(r'[0-9a-f]{64}',record[key]) for key in ('source_sha256','snapshot_sha256'))):
                raise ValueError('INVALID_INGEST_JOURNAL')
            target=self.path/(sid+'.json');source=self.originals/(sid+record['extension'])
            if target.is_symlink() or source.is_symlink():raise ValueError('INVALID_INGEST_JOURNAL')
            if target.exists():
                if (sha256(target.read_bytes()).hexdigest()!=record['snapshot_sha256'] or not source.is_file()
                    or sha256(source.read_bytes()).hexdigest()!=record['source_sha256']):
                    raise ValueError('INGEST_JOURNAL_CONFLICT')
            elif source.exists():
                if sha256(source.read_bytes()).hexdigest()!=record['source_sha256']:raise ValueError('INGEST_JOURNAL_CONFLICT')
                source.unlink()
            marker.unlink()

    def _stamp_snapshot(self,sid):
        self._next_snapshot_epoch+=1
        self._snapshot_epochs[sid]=self._next_snapshot_epoch
        self._index_views.refresh_epochs(self._snapshot_epochs)
    def _build_fields(self,docs,blocks,base=None):
        frozen_blocks=freeze(blocks)
        allblocks=[b for snap in sorted(frozen_blocks) for b in frozen_blocks[snap]]
        reusable=set()
        if base and getattr(base.index,'chunk_cap',None)==self.cap:
            reusable={sid for sid in frozen_blocks if sid in base.blocks and frozen_blocks[sid]==base.blocks[sid] and docs[sid]==base.docs[sid]}
        by_snapshot=defaultdict(list)
        if base:
            for chunk in base.chunks:by_snapshot[chunk['snapshot_id']].append(chunk)
        chunks=freeze([chunk for sid in sorted(frozen_blocks) for chunk in
            (by_snapshot[sid] if sid in reusable else make_chunks(frozen_blocks[sid],self.cap))])
        dense_base=base.dense if base else self.dense
        by_block=freeze({b['block_id']:b for b in allblocks})
        if len(by_block)!=len(allblocks):raise ValueError('DUPLICATE_BLOCK_IDS')
        dimensions=getattr(dense_base,'vector_dim',1024) if self.retriever_mode in ('dense','hybrid') else 0
        estimate=4*sum(len(b['text'].encode('utf-8'))+256 for b in allblocks)+4*sum(len(c['index_text'].encode('utf-8'))+256 for c in chunks)+len(chunks)*dimensions*40
        self._index_views.check_capacity(estimate)
        lexical=LexicalIndex.from_previous(chunks,base.index) if base else LexicalIndex(chunks)
        lexical.chunk_cap=self.cap
        lexical.chunks=tuple(lexical.chunks);lexical.lengths=tuple(lexical.lengths)
        lexical.frequencies=tuple(FrozenCounts(tf) for tf in lexical.frequencies);lexical.df=FrozenCounts(lexical.df)
        gate=EvidenceGate(allblocks)
        gate.blocks=freeze(gate.blocks);gate.duplicate_block_ids=frozenset(gate.duplicate_block_ids)
        from .structure import StructureCatalog
        structure=StructureCatalog.from_previous(frozen_blocks,base.structure,reusable) if base else StructureCatalog(allblocks)
        for key,value in vars(structure).items():
            if isinstance(value,(dict,list)):setattr(structure,key,freeze(value))
        dense=None
        if self.retriever_mode in ('dense','hybrid'):
            if dense_base is None:
                from .retrieval import DenseQdrantLocal
                dense=DenseQdrantLocal()
                try:dense.rebuild(chunks)
                except BaseException:
                    close=getattr(dense,'close',None)
                    if close:close()
                    raise
            elif hasattr(dense_base,'fork_for_chunks'):dense=dense_base.fork_for_chunks(chunks)
            else:
                # Lightweight test adapters may use a copied data owner. Real
                # encoder/client adapters must explicitly implement safe forking.
                if hasattr(dense_base,'client') or hasattr(dense_base,'encoder'):raise RuntimeError('DENSE_FORK_REQUIRED')
                dense=copy.copy(dense_base);dense.rebuild(chunks)
        try:
            return dict(docs=freeze(docs),blocks=frozen_blocks,chunks=chunks,by_block=by_block,
                index=lexical,gate=gate,structure=structure,dense=dense,_snapshot_epochs=freeze(dict(base._snapshot_epochs if base else self._snapshot_epochs)),estimated_bytes=estimate)
        except BaseException:
            close=getattr(dense,'close',None)
            if close:close()
            raise

    def _publish_fields(self,fields,*,replace_data=False,prune_cache=True):
        try:self._index_views.publish(**fields)
        except BaseException:
            close=getattr(fields['dense'],'close',None)
            if close:close()
            raise
        if replace_data:self._adopt_source_data(self._index_views.current)
        self._adopt_view(self._index_views.current)
        scorer=getattr(self,'reranker',None)
        if prune_cache and scorer is not None and hasattr(scorer,'score_cache'):
            scorer.score_cache.prune(fields['chunks']);scorer.score_cache.flush()

    def _rebuild(self):
        self._publish_fields(self._build_fields(self.docs,self.blocks))

    def prepare_addition(self,metadata,blocks,base):
        docs=dict(base.docs);sources=dict(base.blocks);sid=metadata['snapshot_id']
        docs[sid]=metadata;sources[sid]=blocks
        return self._build_fields(docs,sources,base)

    def _adopt_source_data(self,view):
        self.docs=dict(view.docs);self.blocks=dict(view.blocks);self._snapshot_epochs=dict(view._snapshot_epochs)
        self._next_snapshot_epoch=max([self._next_snapshot_epoch,*self._snapshot_epochs.values()])

    def _adopt_view(self,view):
        for key in ('dense','chunks','by_block','index','gate','structure','retrieval_ready'):
            setattr(self,key,getattr(view,key))

    def _restore_view(self,view):
        self._index_views.restore(view)
        self._adopt_view(view)

    def read_view(self):
        with self.lock:return self._index_views.pin()

    def index_view_stats(self):return self._index_views.stats()

    def close(self):
        with self.lock:
            self._index_views.close()
            close=getattr(self.reranker,'close',None)
            if close:close()
    def source_path(self,snapshot_id):
        m=self.docs.get(snapshot_id)
        if not m or not m.get('original_stored'):return None
        ext=Path(m['filename']).suffix.lower()
        if ext not in DocParser.EXTENSIONS:return None
        p=self.originals/(snapshot_id+ext)
        return p if p.is_file() else None

    def ingest(self, name, data, version='', replaces_snapshot_id=None):
        metadata,blocks=DocParser.parse(name,data,version)
        with inference_slot(),self.lock,self._index_views.pin() as previous_view:
            self._index_views.check_capacity()
            if replaces_snapshot_id is not None:
                previous=self.docs.get(replaces_snapshot_id)
                if not previous:
                    raise ValueError('UNKNOWN_REVISION_PARENT')
                if previous['filename'] != metadata['filename']:
                    raise ValueError('REVISION_FILENAME_MISMATCH')
                if previous['snapshot_id'] == metadata['snapshot_id']:
                    raise ValueError('REVISION_UNCHANGED')
                metadata['supersedes_snapshot_id']=replaces_snapshot_id
            sid=metadata['snapshot_id']
            if sid in self.docs:return dict(self.docs[sid], duplicate=True)
            target=self.path/(sid+'.json')
            suffix=Path(metadata['filename']).suffix.lower()
            src_dest=self.originals/(sid+suffix)
            metadata['original_stored']=True
            content=json.dumps({'metadata':metadata,'blocks':blocks},ensure_ascii=False)
            with tempfile.NamedTemporaryFile(mode='wb',dir=self.originals,prefix='.tmp_',delete=False) as f:
                f.write(data);f.flush();os.fsync(f.fileno());tmp_source=Path(f.name)
            with tempfile.NamedTemporaryFile(mode='w',dir=self.path,prefix='.tmp_',suffix='.json',delete=False,encoding='utf-8') as f:
                f.write(content);f.flush();os.fsync(f.fileno());tmp_metadata=Path(f.name)
            journal=self.path/('.tmp_ingest_'+sid+'.journal')
            record={'snapshot_id':sid,'extension':suffix,'source_sha256':sha256(data).hexdigest(),
                    'snapshot_sha256':sha256(content.encode('utf-8')).hexdigest()}
            writing=journal.with_suffix('.journal.writing')
            try:
                with writing.open('x',encoding='utf-8') as f:
                    json.dump(record,f);f.flush();os.fsync(f.fileno())
                os.replace(writing,journal)
                os.replace(tmp_source,src_dest)
                os.replace(tmp_metadata,target)
            except Exception:
                # A failed metadata commit is not a successful ingestion.
                if not target.exists():src_dest.unlink(missing_ok=True)
                raise
            finally:
                tmp_source.unlink(missing_ok=True);tmp_metadata.unlink(missing_ok=True)
                writing.unlink(missing_ok=True)
                if not src_dest.exists() or target.exists():journal.unlink(missing_ok=True)
            self.docs[sid]=metadata;self.blocks[sid]=blocks
            try:
                self._rebuild()
            except Exception as exc:
                # Do not retain a committed source when its required Dense index
                # could not be constructed. Local one-process rollback only.
                self.docs.pop(sid,None);self.blocks.pop(sid,None)
                target.unlink(missing_ok=True);src_dest.unlink(missing_ok=True)
                if self._index_views.current is not previous_view:self._restore_view(previous_view)
                raise RetrieverBuildFailed('RETRIEVER_INDEX_BUILD_FAILED') from exc
            self._stamp_snapshot(sid)
            return dict(metadata, duplicate=False)
    def delete(self, snapshot_id: str) -> bool:
        """Single-process local-demo deletion. Not an ACID/multi-worker transaction."""
        if not re.fullmatch(r'[0-9a-f]{28}',snapshot_id):
            return False
        with inference_slot(),self.lock,self._index_views.pin() as previous_view:
            if snapshot_id not in self.docs:
                return False
            self._index_views.check_capacity()
            src=self.source_path(snapshot_id)
            metadata_path=self.path/(snapshot_id+'.json')
            # Hide snapshot from restart loading before updating the in-memory index.
            tombstone=self.path/(snapshot_id+'.deleting')
            if tombstone.exists():
                raise RuntimeError('DELETE_RECOVERY_REQUIRED')
            os.replace(metadata_path,tombstone)
            old_meta=self.docs.pop(snapshot_id)
            old_blocks=self.blocks.pop(snapshot_id)
            try:
                self._rebuild()
                if src is not None:
                    src.unlink()
            except Exception:
                self.docs[snapshot_id]=old_meta
                self.blocks[snapshot_id]=old_blocks
                os.replace(tombstone,metadata_path)
                if self._index_views.current is not previous_view:self._restore_view(previous_view)
                raise
            self._snapshot_epochs.pop(snapshot_id,None)
            self._index_views.refresh_epochs(self._snapshot_epochs)
            tombstone.unlink(missing_ok=True)
            return True

    def query(self, question, snapshot_id=None, mode='evidence_only', *, snapshot_ids=None, trace=None, inspect_only=False, embedding_reuse=True, context_policy=None, generation_policy=None,knowledge_scope=None,overview=False,followup_context=None,window_policy='legacy',overview_sampling='legacy'):
        from .diagnostics import QueryTrace
        import logging
        trace=trace or QueryTrace()
        acquired=False;lease=None;compute=None
        try:
            with trace.stage('queue'):
                waiting=inference_slot(trace.check_cancelled);waiting.__enter__();compute=waiting
                while not self.lock.acquire(timeout=0.2):trace.check_cancelled()
                acquired=True
                if getattr(self,'_recovery_required',False):raise RuntimeError('INDEX_RECOVERY_REQUIRED')
            lease=self._index_views.pin()
            prepared=self._prepare_query_locked(question,snapshot_id,mode,snapshot_ids=snapshot_ids,trace=trace,inspect_only=inspect_only,embedding_reuse=embedding_reuse,context_policy=context_policy,generation_policy=generation_policy,knowledge_scope=knowledge_scope,overview=overview,followup_context=followup_context,window_policy=window_policy,overview_sampling=overview_sampling,read_view=lease.view)
            self.lock.release();acquired=False
            compute.__exit__(None,None,None);compute=None
            if isinstance(prepared,PreparedGeneration):
                trace.check_cancelled()
                result=prepared.run()
                with trace.stage('source_revalidation'):
                    while not self.lock.acquire(timeout=0.2):trace.check_cancelled()
                    acquired=True
                    if any(sid not in self.docs or self._snapshot_epochs.get(sid)!=epoch for sid,epoch in prepared.snapshot_epochs):
                        # A deleted/re-added source cannot authorize an old in-flight
                        # answer, even when its content-derived snapshot ID matches.
                        result={'status':'ABSTAIN_SOURCE_CHANGED','reason':'所选文档在回答期间发生变化，请重新查询。',
                                'safe_output':'','generated_claims':[],'source_extracts':[],
                                'llm_invoked':result.get('llm_invoked',False),
                                'model_support_review_passed':False,'answer_semantics_verified':False,
                                'independent_semantic_gold':False,
                                **{key:prepared.metadata[key] for key in ('context_policy','generation_policy','snapshot_ids','retriever')}}
                    else:result.update(prepared.metadata)
                self.lock.release();acquired=False
            else:result=prepared
            trace.outcome=result.get('status','UNKNOWN')
            result['diagnostics']=trace.export()
            return plain(result)
        except Exception as exc:
            from .diagnostics import QueryCancelled
            trace.outcome='CANCELLED' if isinstance(exc,QueryCancelled) else 'ERROR'
            if not isinstance(exc,QueryCancelled):trace.error(trace.current_stage or 'pipeline',type(exc).__name__)
            raise
        finally:
            if acquired:self.lock.release()
            if compute is not None:compute.__exit__(None,None,None)
            if lease is not None:lease.close()
            logging.getLogger('techdoc.query').info(json.dumps(trace.export(),ensure_ascii=False))

    def _prepare_query_locked(self, question, snapshot_id, mode, *, snapshot_ids, trace, inspect_only=False, embedding_reuse=True,context_policy=None,generation_policy=None,knowledge_scope=None,overview=False,followup_context=None,window_policy='legacy',overview_sampling='legacy',read_view=None):
        data=read_view or self
        if window_policy not in ('legacy','zh_questions_v1'):raise ValueError('BAD_WINDOW_POLICY')
        from .followup import FollowupContext
        followup=FollowupContext.from_dict(followup_context)
        retrieval_topic=followup.retrieval_topic if followup else ''
        context_question=question+' '+retrieval_topic if retrieval_topic else question
        if overview and knowledge_scope is None:raise ValueError('OVERVIEW_REQUIRES_KNOWLEDGE_SCOPE')
        context_policy='legacy' if overview else (context_policy or self.context_policy)
        if context_policy not in ('legacy','structured'):raise ValueError('BAD_CONTEXT_POLICY')
        generation_policy=generation_policy or self.generation_policy
        from .generation import policy_identity
        policy_identity(generation_policy)
        retriever={'bm25':'BM25_LOCAL_DEMO','dense':'DENSE_QDRANT_LOCAL','hybrid':'HYBRID_RRF_LOCAL'}[self.retriever_mode]
        if overview:retriever='DOCUMENT_INTRO_PREVIEW'
        if not data.retrieval_ready:
            return {'status':'RETRIEVAL_UNAVAILABLE','reason':'Configured index is not ready','retriever':retriever}
        if not data.docs:return {'status':'ABSTAIN_NO_DOCUMENTS','reason':'No documents indexed','retriever':retriever}
        if knowledge_scope is not None:
            from .knowledge_scope import KnowledgeQueryScope
            if not isinstance(knowledge_scope,KnowledgeQueryScope) or snapshot_ids is not None or snapshot_id is not None:
                return {'status':'ABSTAIN_BAD_SCOPE','reason':'Invalid or mixed knowledge scope','retriever':retriever}
            selected=knowledge_scope.snapshots
        elif snapshot_ids is not None and snapshot_id is not None:
            return {'status':'ABSTAIN_BAD_SCOPE','reason':'Specify snapshot_id OR snapshot_ids, not both','retriever':retriever}
        elif snapshot_ids is not None:
            if (not isinstance(snapshot_ids,(list,tuple)) or not 1<=len(snapshot_ids)<=4 or
                any(not isinstance(x,str) or not re.fullmatch(r'[0-9a-f]{28}',x) for x in snapshot_ids) or
                len(snapshot_ids)!=len(set(snapshot_ids))):
                return {'status':'ABSTAIN_BAD_SCOPE','reason':'Provide 1-4 unique valid snapshot IDs','retriever':retriever}
            selected=tuple(snapshot_ids)
        elif snapshot_id is not None:
            selected=(snapshot_id,)
        elif len(data.docs)==1:
            selected=(next(iter(data.docs)),)
        else:
            return {'status':'ABSTAIN_DOCUMENT_SCOPE','reason':'Select snapshot_id or explicit snapshot_ids to prevent version mixing','retriever':retriever}
        if any(sid not in data.docs for sid in selected):
            return {'status':'ABSTAIN_UNKNOWN_DOCUMENT','reason':'Unknown document snapshot','retriever':retriever}
        # A single logical document cannot have two versions in one query.
        filenames=[data.docs[sid]['filename'] for sid in selected]
        if knowledge_scope is None and len(filenames)!=len(set(filenames)):
            return {'status':'ABSTAIN_VERSION_CONFLICT','reason':'Choose at most one snapshot per filename','retriever':retriever}
        multi=len(selected)>1
        from .query_plan import QueryPlan
        plan=trace.call('query_plan',QueryPlan.build,question,selected,
            policy=self.query_plan_policy,answer=mode=='answer',
            hybrid=not overview and self.retriever_mode=='hybrid',reranker=not overview and self.reranker is not None,
            retrieval_topic=retrieval_topic,**({'knowledge_scope':knowledge_scope} if knowledge_scope else {}))
        plan_shadow=None
        def dense_hits(part,topk):
            if hasattr(data.dense,'query_many'):
                groups=[selected] if embedding_reuse else [(sid,) for sid in selected]
                values=[h for group in groups for h in trace.call('dense_retrieval',data.dense.query_many,part,group,topk=topk,trace=trace)]
                if any(h['chunk']['snapshot_id'] not in selected for h in values):raise ValueError('DENSE_PAYLOAD_SCOPE_MISMATCH')
                return values
            values=[]
            for sid in selected:
                rows=trace.call('dense_retrieval',data.dense.query,part,sid,topk=topk)
                if any(h['chunk']['snapshot_id']!=sid for h in rows):raise ValueError('DENSE_PAYLOAD_SCOPE_MISMATCH')
                values.extend(rows)
            return values
        facet_candidates={}
        candidate_k=10 if self.reranker else 5
        # Explicit per-source retrieval avoids a larger document starving a
        # smaller document of all hits. Still finite: max 4*10 candidates.
        if overview:
            from .knowledge_scope import overview_intro_hits
            hits=trace.call('lexical_retrieval',overview_intro_hits,data.chunks,knowledge_scope,policy=overview_sampling)
        elif knowledge_scope is not None:
            from .retrieval import fuse_rankings
            from .knowledge_scope import bounded_candidates
            rankings=[]
            for part in plan.facets:
                trace.check_cancelled()
                start=len(rankings)
                if self.retriever_mode in ('bm25','hybrid'):
                    rankings.append(trace.call('lexical_retrieval',data.index.query_scope,part,selected,
                        required_snapshots=knowledge_scope.required_snapshots,topk=32))
                if self.retriever_mode in ('dense','hybrid'):
                    if not hasattr(data.dense,'query_scope'):raise ValueError('KNOWLEDGE_DENSE_SCOPE_UNSUPPORTED')
                    rankings.append(trace.call('dense_retrieval',data.dense.query_scope,part,selected,
                        required_snapshots=knowledge_scope.required_snapshots,topk=32,trace=trace))
                group=rankings[start:]
                pair_budget=64
                budget=pair_budget//len(plan.facets)+(1 if part in plan.facets[:pair_budget%len(plan.facets)] else 0)
                facet_candidates[part]=bounded_candidates(fuse_rankings(group,sum(map(len,group))),knowledge_scope.required_snapshots,budget)
            if any(hit['chunk']['snapshot_id'] not in selected for ranking in rankings for hit in ranking):
                raise ValueError('KNOWLEDGE_RETRIEVAL_SCOPE_MISMATCH')
            hits=bounded_candidates(fuse_rankings(rankings,sum(map(len,rankings))),knowledge_scope.required_snapshots,64)
        elif self.retriever_mode == 'bm25':
            hits=[hit for sid in selected for hit in trace.call('lexical_retrieval',data.index.query,question,sid,topk=candidate_k)]
            hits.sort(key=lambda h:(-h['score'],h['chunk']['chunk_id']))
        elif self.retriever_mode == 'dense':
            hits=dense_hits(question,candidate_k)
            hits.sort(key=lambda h:(-h['score'],h['chunk']['chunk_id']))
        else:
            if mode=='answer':
                from .retrieval import fuse_rankings
                rankings=[]
                for part in plan.facets:
                    for sid in selected:
                        rankings.append(trace.call('lexical_retrieval',data.index.query,part,sid,topk=10))
                    # Keep the original per-source rank lists: flattening them
                    # into one RRF ranking would change each source's rank weight.
                    dense_by_source=dense_hits(part,10)
                    for sid in selected:
                        rankings.append([h for h in dense_by_source if h['chunk']['snapshot_id']==sid])
                hits=fuse_rankings(rankings,20*len(selected))
            else:
                lex=[hit for sid in selected for hit in trace.call('lexical_retrieval',data.index.query,question,sid,topk=10)]
                dense=dense_hits(question,10)
                lex.sort(key=lambda h:(-h['score'],h['chunk']['chunk_id']))
                dense.sort(key=lambda h:(-h['score'],h['chunk']['chunk_id']))
                hits=fuse_rrf(lex,dense,limit=candidate_k*len(selected))
        if self.reranker and not overview and knowledge_scope is None:
            from .knowledge_scope import bounded_candidates
            # A request's budget includes EVERY facet, not 64 pairs per call.
            facets=plan.facets if mode=='answer' else plan.facets[:1]
            original_hits=hits
            for i,part in enumerate(facets):
                budget=64//len(facets)+(i<64%len(facets))
                facet_candidates[part]=(bounded_candidates(original_hits,frozenset(selected),budget)
                    if len(original_hits)>budget else list(original_hits))
            if len(hits)>64:hits=bounded_candidates(hits,frozenset(selected),64)
        rerank_trace=None
        proposed=plan.proposed_candidates(hits)
        capture=plan.policy=='shadow' and len(proposed)<len(hits)
        shadow_hits=None
        if self.reranker and not overview:
            rerank_candidates=facet_candidates.get(plan.facets[0],hits)
            progress_facets=plan.facets if mode=='answer' else plan.facets[:1]
            trace.begin_pairs(sum(len(facet_candidates.get(part,rerank_candidates)) for part in progress_facets))
            capture_kw={'capture_scores':True} if capture else {}
            hits,rerank_trace=trace.call('rerank',self.reranker.rerank,plan.facets[0] if followup else question,rerank_candidates,trace=trace,**capture_kw)
            scored=rerank_trace.pop('_all_ranking',None)
            rerank_trace['pair_budget']=64
            if capture:
                from .query_plan import shadow_ranking
                if scored is None:
                    plan_shadow={'status':'unavailable','reason':'SCORER_DID_NOT_CAPTURE_SCORES'}
                else:shadow_hits=trace.call('query_plan_shadow',shadow_ranking,proposed,scored)
            if mode=='answer':
                from .retrieval import interleave_facets
                facets=plan.facets[1:]
                if facets:
                    rankings=[hits];traces=[]
                    for facet in facets:
                        ranking,facet_meta=trace.call('rerank',self.reranker.rerank,facet,facet_candidates.get(facet,rerank_candidates),trace=trace)
                        rankings.append(ranking);traces.append(facet_meta)
                    hits=interleave_facets(rankings)
                    rerank_trace['facet_rankings']=traces
                    rerank_trace['selection']='round_robin_question_parts'
                    if knowledge_scope is not None:rerank_trace['pair_budget']=64
                    rerank_trace['elapsed_ms']+=sum(t['elapsed_ms'] for t in traces)
        # Select an authentic source window, reserve one excerpt per
        # document in multi-doc mode; fail closed when any lacks evidence.
        candidates, excerpt_details = trace.call('context_build',select_excerpts,
            hits, data.by_block, context_question, limit=4,
            chars_each=260, chars_total=1040,
            prefer_rank=self.reranker is not None,
            window_policy=window_policy,
            required_snapshots=knowledge_scope.required_snapshots if knowledge_scope else (frozenset(selected) if multi else None))
        req=Request(question=question,
                    target_snapshot=None if multi or knowledge_scope else selected[0],
                    target_snapshots=frozenset(selected) if multi or knowledge_scope else None,
                    permitted_snapshots=frozenset(selected),max_excerpt_chars=1500,
                    required_snapshots=knowledge_scope.required_snapshots if knowledge_scope else None,
                    previous_user_question=followup.previous_user_question if followup else None)
        query_gate=data.gate
        if mode=='answer':
            from .context_windows import chunk_context
            context_builder=data.structure.context if context_policy=='structured' else chunk_context
            query_gate,candidates,excerpt_details=trace.call('context_build',context_builder,
                hits,data.chunks,data.by_block,context_question,frozenset(selected),
                window_policy=window_policy,
                **({'required_snapshots':knowledge_scope.required_snapshots} if knowledge_scope else {}))
            if plan.policy=='shadow' and plan_shadow is None:
                if shadow_hits is None:shadow_hits=hits
                same_rank=[h['chunk']['chunk_id'] for h in hits]==[h['chunk']['chunk_id'] for h in shadow_hits]
                if same_rank:shadow_details=excerpt_details
                else:
                    _,_,shadow_details=trace.call('query_plan_shadow',context_builder,
                        shadow_hits,data.chunks,data.by_block,context_question,frozenset(selected),
                        window_policy=window_policy,
                        **({'required_snapshots':knowledge_scope.required_snapshots} if knowledge_scope else {}))
                by_block=data.by_block
                def coverage(details):
                    return sorted({by_block[sp['block_id']]['snapshot_id'] for d in details for sp in d['source_spans']})
                plan_shadow={'status':'compared','baseline_candidates':len(rerank_candidates) if self.reranker and not overview else len(hits),
                    'proposed_candidates':len(proposed),'ranking_equal':same_rank,
                    'context_equal':excerpt_details==shadow_details,
                    'baseline_source_coverage':coverage(excerpt_details),'proposed_source_coverage':coverage(shadow_details),
                    'baseline_chunk_ids':[h['chunk']['chunk_id'] for h in hits],
                    'proposed_chunk_ids':[h['chunk']['chunk_id'] for h in shadow_hits],
                    'proposed_excerpt_details':shadow_details,
                    'extra_model_calls':0,'applied_to_answer':False}
        locator_shadow=None
        if plan.policy=='shadow' and not overview:
            from .locator_shadow import LocatorIntent,compare_context
            from .diagnostics import QueryCancelled
            locator=LocatorIntent.build(question,selected,data.docs,knowledge_scope.labels if knowledge_scope else ())
            locator_hits,locator_shadow=locator.propose(hits,data.by_block)
            locator_details=excerpt_details
            if locator_shadow['order_changed']:
                try:
                    if mode=='answer':
                        _,_,locator_details=trace.call('query_plan_shadow',context_builder,
                            locator_hits,data.chunks,data.by_block,context_question,frozenset(selected),
                            window_policy=window_policy,
                            **({'required_snapshots':knowledge_scope.required_snapshots} if knowledge_scope else {}))
                    else:
                        _,locator_details=trace.call('query_plan_shadow',select_excerpts,
                            locator_hits,data.by_block,context_question,limit=4,chars_each=260,chars_total=1040,
                            prefer_rank=self.reranker is not None,window_policy=window_policy,
                            required_snapshots=knowledge_scope.required_snapshots if knowledge_scope else (frozenset(selected) if multi else None))
                except QueryCancelled:raise
                except Exception:
                    # Shadow-only diagnostics must not replace a valid baseline
                    # answer or publish proposed text when observation fails.
                    locator_shadow.update(status='shadow_context_unavailable',reason_code='SOURCE_CONTEXT_CHECK_FAILED')
                    locator_details=None
            if locator_details is not None:
                locator_shadow=compare_context(locator_shadow,excerpt_details,locator_details,data.by_block,
                    knowledge_scope.required_snapshots if knowledge_scope else (frozenset(selected) if multi else frozenset()))
        work=None
        if inspect_only:
            if mode!='answer':raise ValueError('INSPECTION_REQUIRES_ANSWER_PIPELINE')
            gated=trace.call('evidence_gate',query_gate.evaluate,req,candidates)
            result={'status':'RETRIEVAL_INSPECTION_ONLY' if gated.status=='EVIDENCE_ONLY' else gated.status,
                    'reason':'Retrieval diagnostics only; no answer generated','safe_output':'',
                    'generated_claims':[],'source_extracts':gated.extracts,'llm_invoked':False,
                    'model_support_review_passed':False,'independent_semantic_gold':False}
        elif mode in ('llm','answer'):
            import os
            base=os.getenv('LLM_BASE_URL');name=os.getenv('LLM_MODEL');key=os.getenv('LLM_API_KEY')
            if not (base and name and key):
                return {'status':'LLM_NOT_CONFIGURED','reason':'Set LLM_BASE_URL, LLM_MODEL and LLM_API_KEY explicitly',
                        'retriever':retriever,'llm_invoked':False}
            client=OpenAIChatClient(model=name,base_url=base,api_key=key,
                                    output_mode=os.getenv('LLM_OUTPUT_MODE','json_schema'),trace=trace,
                                    timeout_s=self.llm_timeout_s)
            if mode=='answer':
                from .generation import GroundedGenerator
                names={r[0]:(r[1],r[2]) for r in knowledge_scope.labels} if knowledge_scope else {}
                source_documents={sid:dict(data.docs[sid],
                    **({'filename':names[sid][0] or data.docs[sid]['filename'],'product_version':names[sid][1]}
                       if sid in names else {})) for sid in selected}
                engine=GroundedGenerator(query_gate,client,trace=trace,policy=generation_policy,
                                         source_documents=source_documents)
                work=lambda:engine.run(req,candidates)
            else:
                engine=S3BEngine(query_gate,client)
                work=lambda:trace.call('generation',engine.run,req,candidates).as_dict()
            result={}
        else:
            result=data.gate.evaluate(req,candidates).as_dict()
            if result['status']=='EVIDENCE_ONLY':
                result['safe_output']=result.pop('draft')
        result.update({'retriever':retriever,'excerpt_limit_chars':260,'evidence_is_preview_not_answer':True,'dense_runtime_enabled':not overview and self.retriever_mode in ('dense','hybrid'), 'dense_quality_verified':False, 'dense_verified':False,
                       'reranker_enabled':not overview and self.reranker is not None, 'rerank_trace':rerank_trace,
                       'answer_semantics_verified':False,
                       'query_plan':plan.public(),'query_plan_shadow':plan_shadow,'locator_shadow':locator_shadow,
                       'context_policy':context_policy,'structure':data.structure.stats(),
                       'generation_policy':generation_policy,
                       'index_generation':read_view.generation_id if read_view else None,
                       'followup_recipe':'separate_v1' if followup else 'legacy',
                       'window_policy':window_policy,
                       'overview_sampling':overview_sampling if overview else None,
                       'snapshot_id':selected[0] if not multi else None,
                       'snapshot_ids':list(selected),
                       'multi_document_scope_explicit':multi,
                       'ranked_chunk_count':len(hits),
                       'source_download_path':f'/documents/{selected[0]}/source' if not multi and self.source_path(selected[0]) else None,
                       'source_download_paths':{sid:f'/documents/{sid}/source' for sid in selected if self.source_path(sid)},
                       'parser_warnings':sorted({w for sid in selected for w in data.docs[sid]['warnings']}),
                       'excerpt_selection':'ranked_source_block_window_v1',
                       'excerpt_budget_chars_total':1040,
                       'excerpt_details':excerpt_details})
        if mode=='answer':
            result['evidence_is_preview_not_answer']=inspect_only
            result['excerpt_selection']='structured_source_windows_v1' if context_policy=='structured' else 'validated_chunk_windows_v2'
            result['excerpt_budget_chars_total']=3500
            result['excerpt_limit_chars']=600
        if knowledge_scope is not None:
            result.update(knowledge_scope.public())
            result['source_download_path']=None
            result['source_download_paths']={}
        if work is not None:
            return PreparedGeneration(work,result,tuple((sid,data._snapshot_epochs[sid]) for sid in selected))
        return result
