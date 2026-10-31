"""Server-built allowed/required scope, separate from the legacy snapshot contract."""
from dataclasses import dataclass
import re

MAX_ALLOWED_SOURCES = 2048
CONTRACT = 'kb_scope_v1'


@dataclass(frozen=True)
class KnowledgeQueryScope:
    versions: tuple[tuple[str,str,str], ...]  # logical document, version, snapshot
    required_documents: tuple[str, ...] = ()
    labels: tuple[tuple[str,str,str], ...] = ()

    def __post_init__(self):
        if (not 1<=len(self.versions)<=MAX_ALLOWED_SOURCES
                or any(len(item)!=3 or any(not isinstance(v,str) or not v for v in item) for item in self.versions)
                or any(not re.fullmatch('[0-9a-f]{28}',item[2]) for item in self.versions)):
            raise ValueError('BAD_KNOWLEDGE_SCOPE')
        docs=[r[0] for r in self.versions]; snapshots=[r[2] for r in self.versions]
        if len(docs)!=len(set(docs)) or len(snapshots)!=len(set(snapshots)):
            raise ValueError('KNOWLEDGE_SCOPE_MIXED_OR_DUPLICATE_VERSION')
        required=self.required_documents
        if len(required)>4 or len(required)!=len(set(required)) or not set(required)<=set(docs):
            raise ValueError('BAD_REQUIRED_DOCUMENTS')
        if any(len(r)!=3 or r[0] not in snapshots or not isinstance(r[1],str) or not isinstance(r[2],str) for r in self.labels):
            raise ValueError('BAD_SOURCE_LABELS')
        if len({r[0] for r in self.labels})!=len(self.labels):raise ValueError('BAD_SOURCE_LABELS')

    @classmethod
    def from_resolved(cls,scope):
        if scope.get('scope_contract_version')!=CONTRACT:
            raise ValueError('UNKNOWN_SCOPE_CONTRACT')
        return cls(tuple((r['document_id'],r['version_id'],r['snapshot_id']) for r in scope['allowed_versions']),
                   tuple(scope.get('required_document_ids',[])),
                   tuple((r['snapshot_id'],r.get('display_name',''),r.get('version_label','')) for r in scope['allowed_versions']))

    @property
    def snapshots(self): return tuple(r[2] for r in self.versions)

    @property
    def required_snapshots(self):
        return frozenset(r[2] for r in self.versions if r[0] in self.required_documents)

    def public(self):
        return {'scope_contract_version':CONTRACT,'allowed_document_count':len(self.versions),
                'required_document_ids':list(self.required_documents),'candidate_budget':64,
                'rerank_pair_budget':64,'context_window_budget':8,'context_chars_budget':3500}


def bounded_candidates(hits,required,limit=64):
    """Reserve required-source candidates, then spend the remaining global budget."""
    first=[];rest=[];counts={};seen=set()
    for hit in hits:
        ch=hit['chunk'];key=ch['chunk_id'];sid=ch['snapshot_id']
        if key in seen: continue
        seen.add(key)
        if sid in required and counts.get(sid,0)<4:
            first.append(hit);counts[sid]=counts.get(sid,0)+1
        else:rest.append(hit)
    return (first+rest)[:limit]


def representative_body(chunk):
    """Conservative structure hints; never rewrite or manufacture source text."""
    text=chunk['index_text'].replace('\u200b','')
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines:return False
    toc=sum(bool(re.search(r'(?:\.{2,}|…{2,}|·{2,})\s*\d+\s*$|\S\s+\d{1,4}\s*$',line)) for line in lines)
    if len(lines)>=4 and toc>=max(3,len(lines)//2):return False
    headings=[line for line in lines if len(line)<=100 and re.match(r'^(?:\d+(?:\.\d+){0,4}[、.．]?\s+|第[一二三四五六七八九十\d]+[章节部分])\S',line)]
    body=[line for line in lines if line not in headings]
    prose=any(len(line)>=28 and re.search(r'[，。；：:,.!?]',line) for line in body)
    explained_list=sum(len(line)>=12 and bool(re.search(r'[：:]',line)) for line in body)>=2
    # A heading tacked on at a chunk's end does not make the preceding example
    # its section body; require the section to start near the beginning.
    return bool(any(line in headings for line in lines[:2]) and (prose or explained_list))


def overview_intro_hits(chunks,scope,*,policy='legacy'):
    """Bounded source previews; prioritise required documents, never claim full reading."""
    ordered=sorted(scope.required_snapshots)+[s for s in scope.snapshots if s not in scope.required_snapshots]
    if policy not in ('legacy','representative_v1'):raise ValueError('BAD_OVERVIEW_SAMPLING')
    selected=set(ordered[:4]);groups={s:[] for s in selected};representative={}
    for chunk in chunks:
        sid=chunk['snapshot_id']
        if sid not in selected:continue
        first=not groups[sid]
        if len(groups[sid])<2:groups[sid].append(chunk)
        if policy=='representative_v1' and not first and sid not in representative and representative_body(chunk):representative[sid]=chunk
    if policy=='representative_v1':
        for sid,chunk in representative.items():groups[sid][1]=chunk
    return [{'score':1.0,'chunk':groups[s][i]} for i in range(2) for s in ordered[:4] if len(groups[s])>i]
